"""Lossless event-stream storage; logical manifest bytes and run identity stay stable."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import zlib

EVENTS = 'events.jsonl'
PACKED_EVENTS = 'events.jsonl.gz'
CHUNK = 1024 * 1024


@contextmanager
def open_event_bytes(run: Path):
    """Read old plain captures and gzip captures without extracting receipts."""
    for name in (EVENTS, PACKED_EVENTS):
        path = Path(run) / name
        if path.is_symlink():
            raise ValueError('Capture files may not be symlinks')
        try:
            source = path.open('rb')
        except FileNotFoundError:
            continue
        with source:
            if name == PACKED_EVENTS:
                try:
                    with gzip.GzipFile(fileobj=source, mode='rb') as stream:
                        yield stream
                except (EOFError, gzip.BadGzipFile, zlib.error):
                    raise ValueError('Compressed capture is corrupt') from None
            else:
                yield source
        return
    raise FileNotFoundError('Capture event stream is missing')


@contextmanager
def open_events(run: Path):
    with open_event_bytes(run) as stream:
        with io.TextIOWrapper(stream, encoding='utf-8') as text:
            yield text


def stream_identity(stream, expected_bytes=None):
    count = 0
    checksum = hashlib.sha256()
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        count += len(chunk)
        if expected_bytes is not None and count > expected_bytes:
            raise ValueError('Capture checksum mismatch')
        checksum.update(chunk)
    return {'file': EVENTS, 'bytes': count, 'sha256': checksum.hexdigest()}


def event_identity(run: Path, expected_bytes=None):
    with open_event_bytes(run) as stream:
        return stream_identity(stream, expected_bytes)


def write_events(run: Path, events):
    """Write gzip directly; hash exactly the logical JSONL bytes in the manifest."""
    path = Path(run) / PACKED_EVENTS
    count = 0
    checksum = hashlib.sha256()
    with path.open('xb') as output:
        os.fchmod(output.fileno(), 0o600)
        with gzip.GzipFile(filename='', fileobj=output, mode='wb', mtime=0) as stream:
            for event in events:
                data = (json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
                stream.write(data)
                count += len(data)
                checksum.update(data)
        output.flush()
        os.fsync(output.fileno())
    return {'file': EVENTS, 'bytes': count, 'sha256': checksum.hexdigest()}


def _sync_directory(run):
    fd = os.open(run, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def repack_run(run: Path, *, restore=False):
    """Replace physical encoding only after both old and new bytes match the manifest.

    The logical manifest is untouched, preserving ledger run IDs and checksums.
    An interrupted conversion leaves either a complete original or two copies.
    This checks transport integrity, not semantic validity of the original capture.
    """
    from .capture import FILES, SCHEMA_VERSION
    run = Path(run)
    info = run.lstat()
    if (not stat.S_ISDIR(info.st_mode) or run.is_symlink()
            or info.st_uid != os.getuid() or info.st_mode & 0o077
            or run.name.startswith('.')):
        raise ValueError('Only finalized private capture directories can be repacked')
    lock_path = run / '.capture-storage.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest_path = run / 'manifest.json'
        if manifest_path.is_symlink():
            raise ValueError('Manifest may not be a symlink')
        manifest = json.loads(manifest_path.read_text())
        rows = manifest['files']
        names = {r['file'] for r in rows}
        if (manifest.get('schema_version') != SCHEMA_VERSION or len(rows) != len(names)
                or names not in (FILES, FILES | {'agents.json'})):
            raise ValueError('Unexpected capture file manifest')
        entry = next(r for r in rows if r['file'] == EVENTS)
        if type(entry['bytes']) is not int or entry['bytes'] < 0:
            raise ValueError('Invalid capture byte count')
        # Check all logical checksums, not only the event stream being converted.
        for row in rows:
            if row['file'] == EVENTS:
                actual = event_identity(run, entry['bytes'])
            else:
                path = run / row['file']
                if path.is_symlink():
                    raise ValueError('Capture files may not be symlinks')
                with path.open('rb') as source:
                    actual = stream_identity(source, row['bytes'])
            if actual['bytes'] != row['bytes'] or actual['sha256'] != row['sha256']:
                raise ValueError('Capture checksum mismatch')
        source = run / (PACKED_EVENTS if restore else EVENTS)
        target = run / (EVENTS if restore else PACKED_EVENTS)
        if not source.exists():
            return {'changed': False, 'logical_bytes': entry['bytes'], 'stored_bytes': target.stat().st_size}
        if source.is_symlink() or target.is_symlink():
            raise ValueError('Capture files may not be symlinks')
        before = source.stat().st_size + (target.stat().st_size if target.exists() else 0)
        handle, temporary = tempfile.mkstemp(prefix='.event-storage-', dir=run)
        try:
            with os.fdopen(handle, 'wb') as output:
                with (gzip.open(source, 'rb') if restore else source.open('rb')) as input_stream:
                    if restore:
                        writer = output
                    else:
                        writer = gzip.GzipFile(filename='', fileobj=output, mode='wb', mtime=0)
                    try:
                        count = 0
                        digest = hashlib.sha256()
                        while True:
                            block = input_stream.read(CHUNK)
                            if not block:
                                break
                            count += len(block)
                            if count > entry['bytes']:
                                raise ValueError('Capture checksum mismatch')
                            digest.update(block)
                            writer.write(block)
                    finally:
                        if writer is not output:
                            writer.close()
                if count != entry['bytes'] or digest.hexdigest() != entry['sha256']:
                    raise ValueError('Capture checksum mismatch')
                output.flush()
                os.fsync(output.fileno())
            with (open(temporary, 'rb') if restore else gzip.open(temporary, 'rb')) as check:
                actual = stream_identity(check, entry['bytes'])
            if actual['bytes'] != entry['bytes'] or actual['sha256'] != entry['sha256']:
                raise ValueError('Converted capture checksum mismatch')
            # Any existing target is from a previous interrupted conversion;
            # replace it only with the newly verified identical logical bytes.
            os.replace(temporary, target)
            _sync_directory(run)
            source.unlink()
            _sync_directory(run)
            return {'changed': True, 'logical_bytes': entry['bytes'],
                    'stored_bytes': target.stat().st_size,
                    'bytes_saved': before - target.stat().st_size}
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except (EOFError, gzip.BadGzipFile, zlib.error):
        raise ValueError('Compressed capture is corrupt') from None
    finally:
        os.close(fd)
